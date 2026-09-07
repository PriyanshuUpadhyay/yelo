"""Updates preserve local work; releases contain only committed source."""

import hashlib
import subprocess
import tarfile

import pytest

from jello import maintenance


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Release test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "release@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Release test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "release@example.test")
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "pyproject.toml").write_text('[project]\nname = "jello"\nversion = "1.2.3"\n')
    (root / "src/jello").mkdir(parents=True)
    (root / "src/jello/__init__.py").write_text('__version__ = "1.2.3"\n')
    (root / "apps/UsageHUD").mkdir(parents=True)
    (root / "apps/UsageHUD/Package.swift").write_text("// release fixture\n")
    (root / ".gitignore").write_text("dist/\nprivate.txt\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Release fixture")
    return root


def test_release_includes_swift_source_and_excludes_ignored_files(repository):
    (repository / "private.txt").write_text("not for release")
    git(repository, "tag", "v1.2.3")
    maintenance.release(repository, repository / "dist", "v1.2.3")
    archive = repository / "dist/jello-1.2.3.tar.gz"
    with tarfile.open(archive) as package:
        assert "jello-1.2.3/apps/UsageHUD/Package.swift" in package.getnames()
        assert all(not name.endswith("private.txt") for name in package.getnames())
    assert (archive.parent / "jello-1.2.3.sha256").read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    with pytest.raises(maintenance.MaintenanceError, match="already exist"):
        maintenance.release(repository, repository / "dist")


@pytest.mark.parametrize("command", ["release", "update"])
def test_local_changes_stop_maintenance_before_any_write(repository, command):
    path = repository / "pyproject.toml"
    path.write_text(path.read_text() + "# local work\n")
    before = path.read_bytes()
    with pytest.raises(maintenance.MaintenanceError, match="checkout's changes"):
        if command == "release":
            maintenance.release(repository, repository / "dist")
        else:
            maintenance.update(repository)
    assert path.read_bytes() == before
    assert not (repository / "dist").exists()


def test_release_rejects_wrong_tag(repository):
    with pytest.raises(maintenance.MaintenanceError, match="release tag"):
        maintenance.release(repository, repository / "dist", "v2.0.0")
    assert not (repository / "dist").exists()


def test_release_rejects_inconsistent_version(repository):
    (repository / "src/jello/__init__.py").write_text('__version__ = "0.1.0"\n')
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Mismatched version")
    with pytest.raises(maintenance.MaintenanceError, match="versions must match"):
        maintenance.release(repository, repository / "dist")


def test_update_requires_an_upstream(repository):
    with pytest.raises(maintenance.MaintenanceError, match="upstream"):
        maintenance.update(repository)


def test_update_fast_forwards_and_uses_new_cli_for_setup(repository, tmp_path, monkeypatch):
    install = tmp_path / "installed"
    subprocess.run(["git", "clone", str(repository), str(install)], check=True, capture_output=True)
    (repository / "new.txt").write_text("new release\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "New release")
    monkeypatch.setattr(maintenance.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(maintenance.hud, "launcher_path", lambda: "/fixture/jello")
    calls = []
    original_run = maintenance.run_command
    def run(command):
        calls.append([str(value) for value in command])
        if command[0] == "git":
            original_run(command)
    monkeypatch.setattr(maintenance, "run_command", run)
    maintenance.update(install, check=True)
    assert calls == []
    assert not (install / "new.txt").exists()
    maintenance.update(install)
    assert (install / "new.txt").read_text() == "new release\n"
    assert calls[1:] == [
        ["/fixture/uv", "tool", "install", "--reinstall", "--editable", str(install)],
        ["/fixture/jello", "setup"],
    ]


def test_failed_hud_build_leaves_running_hud_alone(repository, tmp_path, monkeypatch):
    install = tmp_path / "installed"
    subprocess.run(["git", "clone", str(repository), str(install)], check=True, capture_output=True)
    binary = tmp_path / "UsageHUD"
    binary.touch()
    monkeypatch.setattr(maintenance.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(maintenance.hud, "launcher_path", lambda: "/fixture/jello")
    monkeypatch.setattr(maintenance.hud, "binary_path", lambda home: str(binary))
    monkeypatch.setattr(maintenance.hud, "job_state", lambda: (True, 123))
    calls = []
    def run(command):
        calls.append(command)
        if command[-2:] == ["hud", "install"]:
            raise maintenance.MaintenanceError("build failed")
    monkeypatch.setattr(maintenance, "run_command", run)
    with pytest.raises(maintenance.MaintenanceError, match="build failed"):
        maintenance.update(install, with_hud=True)
    assert not any(command[-2:] == ["hud", "stop"] for command in calls)
