"""Claude profile homes mirror the shared home without moving account-local state."""

import json
import os
from pathlib import Path
import stat

from conftest import tree_digest
from yelo.profile import mirror


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_sync_is_idempotent_and_uses_relative_links(tmp_path):
    home = tmp_path / "home"
    shared = home / ".claude"
    shared.mkdir(parents=True)
    (shared / "settings.json").write_text("{}\n")
    (shared / ".update.lock").write_text("locked\n")
    (shared / ".usage-cache.json").write_text("shared junk\n")
    (shared / ".claude.json").write_text("not the account config\n")
    (home / ".claude.json").write_text(
        '{"oauthAccount":{"id":"base"},"theme":"dark","trusted":true}\n')

    assert mirror.sync("sid", str(home)) == []
    profile = shared / ".profiles" / "sid"
    assert mode(profile) == 0o700
    assert os.readlink(profile / "settings.json") == "../../settings.json"
    assert os.readlink(profile / ".update.lock") == "../../.update.lock"
    assert not (profile / ".usage-cache.json").exists()
    assert json.loads((profile / ".claude.json").read_text()) == {
        "theme": "dark", "trusted": True}
    assert mode(profile / ".claude.json") == 0o600

    before = tree_digest(home)
    assert mirror.sync("sid", str(home)) == []
    assert tree_digest(home) == before

    invalid = b"{not json\xff"
    (home / ".claude.json").write_bytes(invalid)
    assert mirror.sync("broken", str(home)) == []
    assert (shared / ".profiles" / "broken" / ".claude.json").read_bytes() == invalid


def test_sync_reports_drift_keeps_config_and_replaces_stale_links(tmp_path):
    home = tmp_path / "home"
    shared = home / ".claude"
    profile = shared / ".profiles" / "sid"
    profile.mkdir(parents=True)
    (shared / "projects").mkdir()
    (shared / "settings.json").write_text("{}\n")
    (home / ".claude.json").write_text('{"new":true}\n')
    (profile / "projects").mkdir()
    (profile / "todos").mkdir()
    (profile / "settings.json").symlink_to("/missing")
    (profile / ".claude.json").write_text('{"old":true}\n')
    (profile / "email").write_text("sid@example.test\n")
    (profile / ".usage-api-cache.json").write_text("{}\n")

    assert mirror.sync("sid", str(home)) == sorted(
        [str(profile / "projects"), str(profile / "todos")])
    assert (profile / "projects").is_dir()
    assert (profile / "todos").is_dir()
    assert os.readlink(profile / "settings.json") == "../../settings.json"
    assert (profile / ".claude.json").read_text() == '{"old":true}\n'
    assert (profile / "email").read_text() == "sid@example.test\n"


def test_profile_sync_reports_drift(yelo):
    home = Path(yelo.home)
    (home / ".claude" / "projects").mkdir()
    drift = home / ".claude" / ".profiles" / "pri" / "projects"
    drift.mkdir()

    result = yelo("profile", "sync", "--cli", "claude")

    assert result.returncode == 1
    assert "synced 2 Claude profiles\n" in result.stdout
    assert f"drift\t{drift}\n" in result.stdout
    assert os.readlink(home / ".claude" / ".profiles" / "work" / "projects") == \
        "../../projects"
