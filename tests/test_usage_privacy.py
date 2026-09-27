"""Passive HUD reads cannot start programs or access the Keychain."""

import json
from pathlib import Path

from conftest import build_usage_home, fixture_env, run_yelo


def test_snapshot_reads_local_identity_without_subprocesses(tmp_path):
    home = build_usage_home(tmp_path / "home", 1_900_000_000)
    env = fixture_env(home)
    log = tmp_path / "child.log"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("security", "claude", "codex"):
        script = stubs / name
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> '{log}'\nexit 1\n")
        script.chmod(0o755)
    env["AGENT_PROFILES_SECURITY_BIN"] = str(stubs / "security")
    env["PATH"] = f"{stubs}:{env['PATH']}"
    result = run_yelo(["usage", "show", "--json"], env)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert {row["provider"] for row in rows} == {"claude", "codex"}
    assert {row["label"] for row in rows} == {
        "cl·pri@example.test", "cl·work@example.test",
        "cx·base@example.test", "cx·alt@example.test",
    }
    assert any(row.get("pct") == 43 for row in rows)
    assert all("primeSignedIn" not in row for row in rows)
    assert not log.exists()


def test_removed_providers_cannot_be_installed(bench):
    for group in ("herdr", "prime"):
        result = bench.run(group, "setup")
        assert result.returncode == 2
    result = bench.run("profile", "create", "--cli", "prime", "example", "--yes")
    assert result.returncode == 2
    assert not (bench.home / ".prime").exists()


def test_setup_leaves_old_prime_launchers_alone(bench):
    bench.launchers.mkdir(parents=True)
    launcher = bench.launchers / "prime-agent-work"
    content = "#!/bin/sh\n# written by yelo setup launchers: account work\nexit 0\n"
    launcher.write_text(content)
    assert bench.run("setup").returncode == 0
    assert launcher.read_text() == content


def test_snapshot_names_only_yelo_launchers(tmp_path):
    home = build_usage_home(tmp_path / "home", 1_900_000_000)
    env = fixture_env(home)
    assert run_yelo(["setup", "launchers"], env).returncode == 0
    launchers = Path(home) / ".local" / "bin"
    written = sorted(launchers.iterdir())
    foreign = written[0]
    foreign.write_text("#!/bin/sh\nexit 0\n")
    result = run_yelo(["usage", "show", "--json"], env)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    tagged = {row["launcher"] for row in rows if "launcher" in row}
    assert tagged == {str(path) for path in written[1:]}
