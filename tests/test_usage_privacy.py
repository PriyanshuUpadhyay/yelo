"""Passive HUD reads cannot start programs or read credentials."""
import pathlib

from conftest import build_usage_home
from jello.profile import core
from jello.usage import snapshot


def test_snapshot_reads_usage_without_credentials_or_subprocesses(tmp_path, monkeypatch):
    home = build_usage_home(tmp_path / "home", 1_900_000_000)
    monkeypatch.setattr(core, "HOME", str(home))
    original_read = core.read_json
    def usage_only(path):
        path = pathlib.Path(path)
        assert path.name not in {"auth.json", ".claude.json"}
        assert ".prime" not in path.parts
        return original_read(path)
    monkeypatch.setattr(core, "read_json", usage_only)
    def forbidden(*args, **kwargs):
        raise AssertionError("The HUD must not start a subprocess or read the Keychain")
    monkeypatch.setattr(core.subprocess, "run", forbidden)
    monkeypatch.setattr(core.subprocess, "Popen", forbidden)
    rows = snapshot.snapshot_rows(str(home), now=1_900_000_000)
    assert {row["provider"] for row in rows} == {"claude", "codex"}
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
    content = "#!/bin/sh\n# written by jello setup launchers: account work\nexit 0\n"
    launcher.write_text(content)
    assert bench.run("setup").returncode == 0
    assert launcher.read_text() == content
