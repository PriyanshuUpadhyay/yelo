"""C15-C17, C30 and laws L2, L5: `jello setup` installs its targets and then changes nothing.

Every test runs the real command against a temporary HOME with the XDG roots pinned inside
it, so nothing under the real `$HOME` is read or written (R8), and with
`JELLO_DOTFILES_ROOT` pointed at a fake dotfiles tree, so the ownership verdict is decided
by a symlink the test made rather than by whatever this machine happens to have. The
`bench` fixture, `SEEDED_SETTINGS`, and `tree_digest` live in conftest.py, because the
Herdr and Prime install suites need exactly the same bench.

Two steps install profile roots, named launchers, and the independent shell integration.
"""

import json
import os
import plistlib
import shutil
import stat
import subprocess

import pytest

from jello import setup
from conftest import (SEEDED_SETTINGS, build_fixture_home, env_for,  # noqa: F401
                      run_jello, tree_digest)

HUD_LABEL = "io.github.priyanshuupadhyay.jello-hud"
# The launcher names conftest's fixture account layout produces.
ACCOUNTS = {"claude-pri", "claude-work", "codex-base", "codex-alt"}


def install_hud_targets(home):
    """The two targets `jello doctor` reads for its `hud` row. A HOME without them is
    matrix M4 row 4 -- `hud missing`, exit 1 -- which would drown out what the tests below
    are actually about: the setup steps."""
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / (HUD_LABEL + ".plist")).write_bytes(plistlib.dumps({"Label": HUD_LABEL}))
    macos = home / "Applications" / "UsageHUD.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (macos / "UsageHUD").write_text("binary\n")


def seed_accounts(bench):
    """conftest's fixture account layout, built under the bench HOME."""
    build_fixture_home(bench.home)


def row_detail(result, name):
    return [line.split("\t")[2] for line in result.stdout.splitlines()
            if line.startswith(name + "\t")][0]


def test_setup_idempotent(bench):
    """C15 and L5: the first run applies both steps, the second changes nothing."""
    seed_accounts(bench)
    first = bench.run("setup")
    assert first.returncode == 0, first.stderr
    assert bench.rows(first) == {"launchers": "changed", "profiles": "unchanged"}

    written = {path.name for path in bench.launchers.iterdir()}
    assert written == ACCOUNTS
    for name in written:
        path = bench.launchers / name
        assert path.read_text().splitlines()[0] == "#!/bin/sh"
        assert stat.S_IMODE(path.stat().st_mode) & 0o111, f"{name} is not executable"

    before = bench.digest()
    second = bench.run("setup")
    assert second.returncode == 0, second.stderr
    assert bench.rows(second) == {"launchers": "unchanged", "profiles": "unchanged"}
    assert bench.digest() == before


def test_a_launcher_carries_the_accounts_environment(bench):
    """C09 as ADR 0005 leaves it: the exports each removed wrapper set are the launcher's
    `exec env` line, and the vendor name is bare so PATH answers at run time."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    lines = (bench.launchers / "claude-pri").read_text().splitlines()

    assert lines[1] == "# written by jello setup launchers: account pri"
    profile = os.path.join(str(bench.home), ".claude", ".profiles", "pri")
    assert lines[2] == (
        f"exec env AGENT_PROFILE_LABEL=pri CLAUDE_PROFILE_DIR={profile} "
        f"CLAUDE_SECURESTORAGE_CONFIG_DIR={bench.home}/.claude-pri claude \"$@\"")

    directory = os.path.join(str(bench.home), ".codex-alt")
    assert (bench.launchers / "codex-alt").read_text().splitlines()[2] == (
        f"exec env CODEX_HOME={directory} "
        f"CODEX_CONFIG_PATH={directory}/config.toml codex \"$@\"")



@pytest.mark.parametrize("cli,account", [("claude", "pri"), ("codex", "alt")])
def test_launchers_work_without_jello_on_path(bench, tmp_path, cli, account):
    """Installed account commands need only the shell and vendor CLI, even after uninstall."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / cli).write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$CLAUDE_PROFILE_DIR\" \"$CODEX_HOME\" "
        "\"$@\"\nexit 23\n")
    (vendor / cli).chmod(0o755)
    # There is no Python interpreter, Jello package, or jello-agent in this PATH.
    (vendor / "env").symlink_to(shutil.which("env"))
    arguments = ["--profile", "deep-review", "a prompt with spaces", "$(touch forbidden)"]
    result = subprocess.run([str(bench.launchers / f"{cli}-{account}"), *arguments],
                            capture_output=True, text=True,
                            cwd=tmp_path, env={"PATH": str(vendor)})
    assert result.returncode == 23, result.stderr
    directories = ([str(bench.home / ".claude/.profiles/pri"), ""] if cli == "claude"
                   else ["", str(bench.home / ".codex-alt")])
    assert result.stdout.splitlines() == [*directories, *arguments]
    assert not (tmp_path / "forbidden").exists()


def test_a_file_that_is_not_ours_is_kept(bench):
    """Line 2 is the whole ownership rule: a `claude-pri` somebody else wrote keeps its
    bytes, is named in the report, and never becomes a jello launcher by accident."""
    seed_accounts(bench)
    bench.launchers.mkdir(parents=True, exist_ok=True)
    stranger = bench.launchers / "claude-pri"
    stranger.write_text("#!/bin/sh\necho mine\n")

    result = bench.run("setup", "launchers")
    assert result.returncode == 0, result.stderr
    assert stranger.read_text() == "#!/bin/sh\necho mine\n"
    assert "kept" in row_detail(result, "launchers")
    assert str(stranger) in row_detail(result, "launchers")

    doctor = bench.run("doctor")
    assert bench.rows(doctor)["launchers"] == "ok", \
        "somebody else's command at that name is not jello's fault"
    assert "kept, not a jello launcher" in row_detail(doctor, "launchers")


def test_a_symlink_at_a_wanted_path_is_kept_not_replaced(bench):
    """R8-F2's remaining case, on the setup side: a symlink pointing at a real launcher
    used to read as ours through the link, so `setup launchers` replaced it with a regular
    file. Every symlink at one of these names is somebody else's arrangement."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    real = bench.launchers / "claude-pri"
    body = real.read_text()
    link = bench.launchers / "claude-work"
    link.unlink()
    link.symlink_to(real)

    result = bench.run("setup", "launchers")
    assert result.returncode == 0, result.stderr
    assert link.is_symlink(), "the link must survive `setup launchers`"
    assert link.resolve() == real.resolve()
    assert real.read_text() == body, "and its target must be untouched"
    assert f"kept, not a jello launcher: {link}" in row_detail(result, "launchers")

    doctor = bench.run("doctor")
    assert bench.rows(doctor)["launchers"] == "ok", \
        "somebody else's arrangement at that name is not jello's fault"
    assert f"kept, not a jello launcher: {link}" in row_detail(doctor, "launchers")


def test_a_stale_launcher_is_missing_not_ok(bench):
    """The file a shell would run carries the account's directory, so one left behind by a
    moved home is a fault the row has to name."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    path = bench.launchers / "claude-work"
    path.write_text(path.read_text().replace(".profiles/work", ".profiles/work-old"))

    doctor = bench.run("doctor")
    assert bench.rows(doctor)["launchers"] == "missing"
    assert f"stale: {path}" in row_detail(doctor, "launchers")

    assert bench.rows(bench.run("setup", "launchers"))["launchers"] == "changed"
    assert bench.rows(bench.run("doctor"))["launchers"] == "ok"


def test_the_launcher_of_a_deleted_account_is_stale_and_then_removed(bench):
    """R8-F1: an account that leaves the census leaves its command behind, and that command
    still runs. It is jello's own file -- line 2 says so -- so doctor names it and setup
    takes it back."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    orphan = bench.launchers / "claude-work"
    assert orphan.is_file()
    shutil.rmtree(bench.home / ".claude" / ".profiles" / "work")

    doctor = bench.run("doctor")
    assert bench.rows(doctor)["launchers"] == "missing"
    assert f"stale: {orphan}" in row_detail(doctor, "launchers")

    result = bench.run("setup", "launchers")
    assert result.returncode == 0, result.stderr
    assert bench.rows(result)["launchers"] == "changed"
    assert f"removed, its account is gone: {orphan}" in row_detail(result, "launchers")
    assert not orphan.exists()
    assert bench.rows(bench.run("doctor"))["launchers"] == "ok"


def test_a_foreign_file_is_never_removed_as_an_orphan(bench):
    """The removal may only ever touch a file carrying the header. Somebody else's command
    at a name jello does not want is not jello's business at all."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    stranger = bench.launchers / "claude-stranger"
    stranger.write_text("#!/bin/sh\necho mine\n")
    link = bench.launchers / "claude-linked"
    link.symlink_to(bench.launchers / "claude-pri")

    assert bench.rows(bench.run("doctor"))["launchers"] == "ok"
    result = bench.run("setup", "launchers")
    assert bench.rows(result)["launchers"] == "unchanged"
    assert stranger.read_text() == "#!/bin/sh\necho mine\n"
    assert link.is_symlink(), "a symlink is somebody's arrangement, not a launcher we wrote"


def test_every_launcher_goes_when_the_last_account_does(bench):
    """The empty-census path removes too: `no account has a launcher to write` must not
    mean `and the old ones stay`."""
    seed_accounts(bench)
    assert bench.run("setup", "launchers").returncode == 0
    for relative in (".claude/.profiles", ".codex", ".codex-alt", ".prime"):
        shutil.rmtree(bench.home / relative)

    result = bench.run("setup", "launchers")
    assert bench.rows(result)["launchers"] == "changed"
    assert list(bench.launchers.iterdir()) == []
    assert bench.rows(bench.run("doctor"))["launchers"] == "ok"


def test_an_unlabelled_home_gets_no_launcher(bench):
    """The name is the command, so a codex home with no `profile-label` has nothing to be
    called. It stays reachable through `jello profile`, and through plain `codex`."""
    seed_accounts(bench)
    (bench.home / ".codex" / "profile-label").write_text("\n")
    assert bench.run("setup", "launchers").returncode == 0
    assert not (bench.launchers / "codex-base").exists()
    assert (bench.launchers / "codex-alt").exists()


def test_setup_runs_one_named_step(bench):
    seed_accounts(bench)
    result = bench.run("setup", "profiles")
    assert result.returncode == 0, result.stderr
    assert list(bench.rows(result)) == ["profiles"]
    assert not bench.launchers.exists()

    unknown = bench.run("setup", "nope")
    assert unknown.returncode == 1
    assert unknown.stderr == "jello: setup: unknown step: nope\n"


def test_setup_never_writes_the_claude_settings(bench):
    """L2 as ADR 0005 leaves it: `~/.claude/settings.json` is read for one doctor row and
    never written, so a full `setup` cannot touch a byte of it."""
    bench.seed_settings(SEEDED_SETTINGS)
    before = bench.settings.read_text()
    seed_accounts(bench)

    assert bench.run("setup").returncode == 0
    assert bench.settings.read_text() == before


def test_setup_respects_dotfiles_ownership(bench):
    """C17 and M4 row 11: a target dotfiles still owns is reported, not replaced."""
    seed_accounts(bench)
    real_profiles = bench.dotfiles / "home" / ".claude" / ".profiles"
    real_profiles.mkdir(parents=True)
    shutil.rmtree(bench.profiles)
    bench.profiles.symlink_to(real_profiles)
    install_hud_targets(bench.home)
    bench.seed_settings(SEEDED_SETTINGS)

    result = bench.run("setup")
    assert result.returncode == 0, result.stderr
    assert bench.rows(result)["profiles"] == "owned-by-dotfiles"
    assert bench.profiles.is_symlink()

    doctor = bench.run("doctor")
    assert "profiles\towned-by-dotfiles" in doctor.stdout
    assert bench.rows(doctor)["usage"] == "ok"
    assert bench.rows(doctor)["hud"] == "ok"


def test_profile_root_symlinked_elsewhere_is_an_error(bench):
    """M4 row 12: a symlink that is not dotfiles' is nobody's to follow."""
    elsewhere = bench.home / "elsewhere"
    elsewhere.mkdir()
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.symlink_to(elsewhere)

    result = bench.run("setup", "profiles")
    assert result.returncode == 1
    assert result.stderr == (
        f"jello: setup: profile root is a symlink outside ~/dotfiles ({bench.profiles})\n")
    assert bench.profiles.is_symlink()
    assert list(elsewhere.iterdir()) == []


def test_setup_json_output(bench):
    seed_accounts(bench)
    result = bench.run("setup", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [row["step"] for row in report] == list(setup.STEP_NAMES) == \
        ["launchers", "profiles"]


def test_every_account_core_reports_gets_a_launcher(bench):
    """L2, the one census: `jello.launchers` asks `profile/core` for its rows, so an account
    the profile commands list and a launcher on disk cannot disagree."""
    seed_accounts(bench)
    environment = env_for(bench.home, bench.dotfiles, bench.bin)
    listed = set()
    for cli, prefix in (("claude", "claude"), ("codex", "codex")):
        result = run_jello(["profile", "list", "--cli", cli, "--json"], environment)
        assert result.returncode == 0, result.stderr
        listed |= {f"{prefix}-{row['name']}" for row in json.loads(result.stdout)
                   if row["name"]}
    assert listed == ACCOUNTS
    assert bench.run("setup", "launchers").returncode == 0
    assert {path.name for path in bench.launchers.iterdir()} == listed
