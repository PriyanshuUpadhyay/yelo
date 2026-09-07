"""Known obsolete wrappers can migrate without changing foreign shell definitions."""

from pathlib import Path

import pytest

from jello import integration, legacy, setup


def test_setup_migrates_known_shell_and_keeps_backup(bench, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench.home / ".config"))
    path = Path(legacy.shell_path(str(bench.home)))
    path.parent.mkdir(parents=True)
    path.write_text(legacy.old_shell())
    assert integration.shell_owned(str(path))
    assert bench.run("setup", "launchers").returncode == 0
    assert path.read_text() == integration.shell_text(str(bench.home))
    assert path.with_name(path.name + ".before-profile-integration").read_text() == legacy.old_shell()
    assert not integration.missing(str(bench.home))
    before = bench.digest()
    assert bench.run("setup", "launchers").returncode == 0
    assert bench.digest() == before


@pytest.mark.parametrize("symlink", [False, True])
def test_custom_shell_or_symlink_is_preserved(bench, symlink, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench.home / ".config"))
    path = Path(legacy.shell_path(str(bench.home)))
    path.parent.mkdir(parents=True)
    if symlink:
        target = path.with_name("owned-elsewhere")
        target.write_text(legacy.old_shell())
        path.symlink_to(target)
    else:
        path.write_text(legacy.old_shell() + "# user change\n")
    before = path.read_text()
    with pytest.raises(setup.SetupError, match="another owner"):
        integration.apply(str(bench.home))
    assert path.read_text() == before
    assert path.is_symlink() == symlink
