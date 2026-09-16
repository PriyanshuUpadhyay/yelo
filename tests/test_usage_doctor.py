"""C11: the read-only audit, including the assertions ported from the reference's own test.

`~/dotfiles/apps/usage-hud/Tests/UsageScriptsTests/test_usage_hud_doctor.py` drove the
reference by putting a fake `usage-hud-data` on PATH that printed canned rows. There is no
data feed to fake any more, so the same two codex rows come from real cache files under a
temporary HOME; its four assertions are otherwise the originals.
"""

import json
import os
import time

import pytest

from conftest import codex_auth, fixture_env, run_yelo, write
from yelo.usage import doctor as doctor_module

LAUNCHCTL_STUB = '''#!/usr/bin/env python3
"""Stand-in for `launchctl print`: loaded for the labels named in FAKE_LOADED."""
import os, sys

loaded = [name for name in os.environ.get("FAKE_LOADED", "").split(",") if name]
target = sys.argv[-1]
if any(target.endswith("/" + name) for name in loaded):
    print("\\tpid = 4242")
    sys.exit(0)
sys.exit(113)
'''
YELO_LABEL = "io.github.priyanshuupadhyay.yelo-hud"
LEGACY_LABEL = "work.foyer.usage-hud"


def rollout_path(directory):
    return os.path.join(directory, "sessions", "2026", "09", "01",
                        "rollout-2026-09-01T09-00-00-1.jsonl")


def codex_account(home, name, document=None, rollout_age=None, percent=7):
    directory = os.path.join(str(home), ".codex-" + name)
    os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
    write(os.path.join(directory, "auth.json"),
          json.dumps(codex_auth(f"{name}@example.test", "pro", "acc-" + name)) + "\n")
    if document is not None:
        write(os.path.join(directory, ".usage-hud-api-cache.json"), json.dumps(document) + "\n")
    if rollout_age is not None:
        path = rollout_path(directory)
        write(path, json.dumps({"payload": {"rate_limits": {
            "primary": {"used_percent": percent, "window_minutes": 10080,
                        "resets_at": int(time.time()) + 95430},
            "secondary": None}}}) + "\n")
        stamp = time.time() - rollout_age
        os.utime(path, (stamp, stamp))
    return directory


@pytest.fixture
def bench(tmp_path):
    """One codex account current from its API cache, one stale from a rollout -- the two
    rows the reference's own doctor test asserted on."""
    home = tmp_path / "home"
    now = int(time.time())
    codex_account(home, "api", document={
        "rate_limits": {"primary": {"used_percent": 12, "window_minutes": 10080,
                                    "resets_at": now + 95430},
                        "secondary": None},
        "fetched_at": now - 30, "source": "api",
    })
    codex_account(home, "rollout", rollout_age=4000)
    stub = tmp_path / "stubs" / "launchctl"
    write(str(stub), LAUNCHCTL_STUB)
    os.chmod(str(stub), 0o755)
    environment = fixture_env(home)
    environment["PATH"] = f"{stub.parent}:{environment['PATH']}"
    environment["FAKE_LOADED"] = LEGACY_LABEL
    return {"home": home, "env": environment,
            "api_cache": os.path.join(str(home), ".codex-api", ".usage-hud-api-cache.json"),
            "rollout": rollout_path(os.path.join(str(home), ".codex-rollout")),
            "logs": os.path.join(str(home), "Library", "Logs")}


def doctor(bench, **overrides):
    environment = dict(bench["env"])
    environment.update(overrides)
    return run_yelo(["usage", "doctor"], environment)


def touch(path, age):
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def restamp_api_cache(bench, age):
    document = json.loads(open(bench["api_cache"], encoding="utf-8").read())
    document["fetched_at"] = int(time.time() - age)
    write(bench["api_cache"], json.dumps(document) + "\n")


def test_doctor_matrix(bench, capsys):
    """Every verdict the audit exists to produce, with the exit code that goes with it:
    the four assertions ported from the reference's own doctor test, plus the exit rule R4
    changed from the reference's always-0 to 1 on any FAIL."""
    # Some rows stale: one of two quiet is a degraded provider, not a stopped pipeline.
    some = doctor(bench)
    assert some.returncode == 0, some.stderr
    assert "PASS: no row without usable data carries a pct" in some.stdout
    assert "PASS: cx·api@example.test 7d: current from api" in some.stdout
    assert "WARN: cx·rollout@example.test 7d: stale data from rollout" in some.stdout
    assert "row(s) without usable data carry a pct" not in some.stdout
    assert "WARN: 1 of 2 row(s) stale" in some.stdout

    # Healthy: every row current, nothing failed.
    touch(bench["rollout"], 30)
    healthy = doctor(bench)
    assert "PASS: all 2 row(s) current" in healthy.stdout
    assert "FAIL" not in healthy.stdout and healthy.returncode == 0

    # Every window quiet means the writers have stopped: the one freshness FAIL.
    touch(bench["rollout"], 9000)
    restamp_api_cache(bench, 9000)
    every = doctor(bench)
    assert "FAIL: every usage row is stale (2 of 2)" in every.stdout
    assert every.returncode == 1
    restamp_api_cache(bench, 30)

    # A `Bearer ` string in either log generation is a leak, counted and never printed.
    leaked = os.path.join(bench["logs"], "usage-hud.err.log")
    write(leaked, "Authorization: Bearer eyJhbGciOi\n")
    leak = doctor(bench)
    assert "FAIL: 1 pipeline-written file(s) hold possible token material" in leak.stdout
    assert "eyJhbGciOi" not in leak.stdout, "the scan reports counts, never contents"
    assert leak.returncode == 1
    os.unlink(leaked)

    # Liveness: before the cutover the dotfiles job is what draws the HUD, so the yelo job
    # being absent is expected. Only neither job loaded is a FAIL.
    warned = doctor(bench)
    assert f"WARN: {YELO_LABEL} not loaded; the dotfiles job {LEGACY_LABEL}" in warned.stdout
    assert warned.returncode == 0

    running = doctor(bench, FAKE_LOADED=f"{YELO_LABEL},{LEGACY_LABEL}")
    assert f"PASS: {YELO_LABEL} running (pid 4242)" in running.stdout
    assert running.returncode == 0

    neither = doctor(bench, FAKE_LOADED="")
    assert f"FAIL: {YELO_LABEL} not loaded — the usage HUD is not running" in neither.stdout
    assert neither.returncode == 1

    # The pct invariant. Only ok and stale rows carry a last confirmed percentage, so no
    # snapshot the CLI can produce reaches this line -- it is the regression alarm for the
    # row gate itself, and the only way to arm it is to hand the section such a row.
    report = doctor_module.Report()
    doctor_module.check_freshness(
        report, [{"label": "cl·pri", "window": "5h", "state": "offline", "pct": 43}],
        time.time())
    assert report.failed
    assert "FAIL: 1 row(s) without usable data carry a pct" in capsys.readouterr().out


def test_unparseable_cache_warns_without_failing(bench, tmp_path):
    """A bad cache is degraded, not dead: the four caches back each other up."""
    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri", "email"), "p@e\n")
    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri",
                       ".usage-api-cache.json"), "{not json")
    result = doctor(bench)
    assert "WARN: pri/.usage-api-cache.json is not parseable JSON" in result.stdout
    assert result.returncode == 0


def test_non_numeric_window_warns(bench):
    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri", "email"), "p@e\n")
    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri",
                       ".usage-cache.json"),
          json.dumps({"five_hour": {"used_percentage": "43", "resets_at": None}}) + "\n")
    result = doctor(bench)
    assert "WARN: pri/.usage-cache.json: 1 window(s) with a non-numeric" in result.stdout
    assert result.returncode == 0


def test_leak_scan_fails_on_token_material(bench):
    """A `Bearer ` string in a cache or in either log generation is a leak, counted and
    never printed."""
    clean = doctor(bench)
    assert "PASS: no token material in" in clean.stdout and clean.returncode == 0

    logs = os.path.join(str(bench["home"]), "Library", "Logs")
    write(os.path.join(logs, "usage-hud.err.log"), "Authorization: Bearer eyJhbGciOi\n")
    leaked = doctor(bench)
    assert "FAIL: 1 pipeline-written file(s) hold possible token material" in leaked.stdout
    assert "eyJhbGciOi" not in leaked.stdout, "the scan reports counts, never contents"
    assert leaked.returncode == 1

    write(os.path.join(logs, "yelo-hud.out.log"), "accessToken\n")
    both = doctor(bench)
    assert "FAIL: 2 pipeline-written file(s)" in both.stdout


def test_credentials_report_per_profile(bench):
    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri", "email"), "p@e\n")
    missing = doctor(bench)
    assert "WARN: pri: no OAuth credential source" in missing.stdout

    write(os.path.join(str(bench["home"]), ".claude", ".profiles", "pri",
                       ".credentials.json"), "{}\n")
    legacy = doctor(bench)
    assert "WARN: pri: .credentials.json present but no Keychain item" in legacy.stdout

    readable = doctor(bench, AGENT_PROFILES_SECURITY_BIN="/usr/bin/true")
    assert "PASS: pri: OAuth token readable from Keychain" not in readable.stdout, (
        "/usr/bin/true prints no blob, so the token does not resolve")


def test_doctor_writes_nothing(bench):
    """Law L4: the audit is read-only, cache files included."""
    before = {}
    for base, _, names in os.walk(str(bench["home"])):
        for name in names:
            path = os.path.join(base, name)
            before[path] = os.stat(path).st_mtime_ns
    assert doctor(bench).returncode == 0
    assert {path: os.stat(path).st_mtime_ns for path in before} == before
