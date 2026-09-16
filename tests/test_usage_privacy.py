"""Passive HUD reads cannot start programs or access the Keychain."""

from conftest import build_usage_home
from yelo.profile import core
from yelo.usage import snapshot


def test_snapshot_reads_local_identity_without_subprocesses(tmp_path, monkeypatch):
    home = build_usage_home(tmp_path / "home", 1_900_000_000)
    monkeypatch.setattr(core, "HOME", str(home))
    def forbidden(*args, **kwargs):
        raise AssertionError("The HUD must not start a subprocess or read the Keychain")
    monkeypatch.setattr(core.subprocess, "run", forbidden)
    monkeypatch.setattr(core.subprocess, "Popen", forbidden)
    rows = snapshot.snapshot_rows(str(home), now=1_900_000_000)
    assert {row["provider"] for row in rows} == {"claude", "codex"}
    assert {row["label"] for row in rows} == {
        "cl·pri@example.test", "cl·work@example.test",
        "cx·base@example.test", "cx·alt@example.test",
    }
    assert any(row.get("pct") == 43 for row in rows)
    assert all("primeSignedIn" not in row for row in rows)


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
