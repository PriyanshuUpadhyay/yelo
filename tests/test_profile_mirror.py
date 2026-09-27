"""Claude profile homes mirror the shared home without moving account-local state."""

import os
from pathlib import Path

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
