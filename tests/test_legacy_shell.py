"""Known obsolete wrappers can migrate without changing foreign shell definitions."""

from pathlib import Path

import pytest

LEGACY = (Path(__file__).parent / "fixtures" / "old_shell.zsh").read_text()


def test_setup_migrates_known_shell_and_keeps_backup(bench, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench.home / ".config"))
    path = bench.home / ".config" / "yelo" / "shell.sh"
    path.parent.mkdir(parents=True)
    path.write_text(LEGACY)
    assert bench.run("setup", "launchers").returncode == 0
    assert "command yelo profile \"$@\"" in path.read_text()
    assert "profiles.pyz" not in path.read_text()
    assert path.with_name(path.name + ".before-profile-integration").read_text() == LEGACY
    assert bench.rows(bench.run("doctor"))["launchers"] == "ok"
    before = bench.digest()
    assert bench.run("setup", "launchers").returncode == 0
    assert bench.digest() == before


@pytest.mark.parametrize("symlink", [False, True])
def test_custom_shell_or_symlink_is_preserved(bench, symlink, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench.home / ".config"))
    path = bench.home / ".config" / "yelo" / "shell.sh"
    path.parent.mkdir(parents=True)
    if symlink:
        target = path.with_name("owned-elsewhere")
        target.write_text(LEGACY)
        path.symlink_to(target)
    else:
        path.write_text(LEGACY + "# user change\n")
    before = path.read_text()
    result = bench.run("setup", "launchers")
    assert result.returncode == 1
    assert "another owner" in result.stderr
    assert path.read_text() == before
    assert path.is_symlink() == symlink
